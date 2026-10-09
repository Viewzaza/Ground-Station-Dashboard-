"""The station logbook: a lasting record of what happened at the station.

Before the first hardware run somebody will ask who armed, from which machine,
what was refused and by which gate, and what autopilot did about it. Nothing in
the backend could answer. ControlService remembered the origin of its last 64
commands, in memory; PlanExecutor overwrote `disengaged_because` each time; a
lapsed lease, a gate flipping or a restart that quietly left autopilot off left
no trace at all; and docker rotates the backend's own log at 10 MB x 3, behind
a request log that scrolls the warnings out within days.

So this keeps one line per event in GS_DATA_DIR/events/YYYY-MM-DD.jsonl:

  {"id": "<unix_ms>-<n>", "ts": ..., "kind": "control.cmd", "sev": "info",
   "text": "operator: goto az=120.0 el=0.0", "data": {...}}

**Most of it is derived, not reported.** The services already publish their
state to the hub on every change — that is how the dashboard stays live — so
the log subscribes like a browser does and works out what changed between
consecutive frames: a new journal entry is a command, armed going false with no
release is an expired lease, a gate's boolean flipping is a gate change. The
services needed one change between them, ControlService's journal learning
*what* each command was. Every derivation is a pure function over two frames,
which is what lets tests pin exactly which sequence of states reads as which
sentence. Mislabelling is the risk here — "lease expired" written for a release
sends someone looking for a bug that is not there.

The hub's queue is bounded and drops the oldest frame of a type when it is
full, so if this falls behind, log lines are lost — never control.

**The log is history, never permission.** Nothing reads it to decide anything.
A lease, autopilot's state and the gates' freshness are never restored from it:
after a restart it *says* that autopilot was engaged and is now off, and that
is all it does. Re-engaging stays a human decision.

Disk work runs in a worker thread. A record is flushed as it is written, and
control, boot and shutdown records are fsynced, because those are the ones an
investigation after a power cut will want. If the directory cannot be written
the log keeps its in-memory ring and reports itself degraded, rather than
taking anything else down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import traceback
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from ..config import Settings
from ..hub import hub

log = logging.getLogger(__name__)

# What is kept in memory, and served without touching the disk.
RING_SIZE = 2000
# The most one query returns. A day of a busy station is a few hundred lines.
QUERY_MAX = 1000
# How far ahead a change to the plan is worth a line. The plan looks a day
# ahead and re-scores passes all the time; what matters to the person reading
# the log is what changed about the next few hours.
PLAN_WINDOW_H = 6.0
# The same warning, repeated within this window, is one line and then a count.
REPEAT_WINDOW_S = 600.0
# How long the loop may sleep with nothing to do before housekeeping runs.
HOUSEKEEPING_S = 30.0
# After a failed write, how long before the disk is tried again.
DISK_RETRY_S = 60.0
# Request bodies and refusals are small JSON; nothing past this is read.
BODY_CAP = 16 * 1024
# SatNOGS job names remembered for the end of a recording. A day's jobs on
# station 5024 are a few dozen.
JOB_NAMES_KEPT = 200
# The most a record may be dated before it was recorded. A record is filed
# under the day it is dated, but ids and paging go by when it was recorded, so
# this bound is what tells a reader how far back a file can hold a newer id
# without opening it. Within it, a record is dated by when it happened.
# Everything the log derives is noticed well inside it: a lapsed lease within
# a second, a request by the time it returns, a recording within a poll. A
# recording's end seen only once SatNOGS answers again after an outage is
# dated when it was noticed instead, and keeps when it happened as
# `happened_at`.
BACKDATE_MAX = timedelta(hours=1)

# The frames the log is derived from. Subscribing by topic keeps the 1 Hz
# rotator and satpos frames out of its queue.
TOPICS = frozenset({"control", "autopilot", "status", "satnogs", "plan"})

SEVERITY = {"info": 0, "warn": 1, "bad": 2}

# A boot and an engage or disengage are what say whether autopilot was on
# when a run ended. See _load.
AUTOPILOT_MARKERS = frozenset({"boot", "autopilot.engaged", "autopilot.disengaged"})

# Settings whose values are never written anywhere, only whether they are set.
# Broad on purpose: a false positive costs a line of config history, a false
# negative puts a token in a file that gets downloaded and attached to emails.
# Covers the alert integrations' names (ntfy, telegram, webhook) and the
# SatNOGS Network and DB tokens too.
SECRET_NAME = re.compile(
    r"token|password|passwd|secret|key|webhook|ntfy|telegram|auth|credential",
    re.IGNORECASE,
)

# A change to one of these between two boots is a warning rather than a note:
# each one changes what the interlock allows or what the antenna is.
SAFETY_SETTINGS = frozenset({
    "mock", "rotator_control_enabled", "control_lease_s", "gate_guard_s",
    "gate_max_stale_s", "track_deadband_deg", "park_az", "park_el",
    "rotctld_host", "rotctld_port", "rotctld_protocol",
    "rot_limit_min_az", "rot_limit_max_az", "rot_limit_min_el", "rot_limit_max_el",
    "station_id",
})

# Audited requests: everything that can change what the station does.
AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AUDITED_PREFIXES = ("/api/control/", "/api/plan/", "/api/commissioning/", "/api/alerts/")
# The only request-body keys an audit line keeps. An allowlist, so a field a
# later endpoint adds — a token, a note — is not recorded until someone decides
# it should be.
AUDIT_FIELDS = ("az", "el", "norad", "enabled", "eyes_on_mast", "hours")

# Warnings the log already records from the frames it watches. A second line
# saying the same thing in the logger's words would only be noise under
# "faults".
DERIVED_ELSEWHERE = frozenset({
    ("app.services.planner_service", "autopilot ENGAGED"),
    ("app.services.planner_service", "autopilot disengaged: %s"),
    ("app.services.control", "track stopping, gate closed: %s"),
})

# PlanExecutor's own words for an operator pressing DISENGAGE — the one
# stand-down that is not news.
OPERATOR_OFF = "switched off by the operator"

_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^(\d+)-(\d+)$")
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s,;\"'<>]+")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _parse_ts(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: datetime) -> str:
    """Every timestamp the log writes, in one format: UTC, milliseconds.

    One format is what lets ts bounds be compared as strings."""
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _hms(raw: Any) -> str:
    ts = _parse_ts(raw)
    return f"{ts.astimezone(timezone.utc):%H:%M:%S}Z" if ts else "?"


def _hm(raw: Any) -> str:
    ts = _parse_ts(raw)
    return f"{ts.astimezone(timezone.utc):%H:%M}Z" if ts else "?"


def id_key(event_id: Any) -> tuple[int, int]:
    """An event id as something that sorts: (unix_ms, counter)."""
    match = _ID.match(event_id) if isinstance(event_id, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _by_id(record: dict) -> tuple[int, int]:
    return id_key(record.get("id"))


def _id_time(key: tuple[int, int]) -> datetime:
    """When the record with this id key was recorded."""
    return datetime.fromtimestamp(key[0] / 1000, timezone.utc)


def _may_hold_after(day: str, key: tuple[int, int]) -> bool:
    """Whether `day`'s file can hold a record with an id above `key`.

    A file holds the records dated that day, and a record is dated at most
    BACKDATE_MAX before it was recorded, so the newest id a file can hold is
    BACKDATE_MAX past the end of its day. A file whose name is not a day it
    can reason about is assumed to hold anything."""
    try:
        end = (datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
               + timedelta(days=1) + BACKDATE_MAX)
    except (ValueError, OverflowError):
        return True
    return int(end.timestamp() * 1000) > key[0]


def _clean(value: Any) -> Any:
    """Text every encoder will take.

    A lone UTF-16 surrogate is valid JSON ("\\ud800"), so a request body can
    carry one, and a setting from an environment variable whose bytes are not
    UTF-8 arrives as some. No UTF-8 encoder will write one: kept as it came,
    one record stopped the writer, every WebSocket the log frame went out on,
    and every query whose answer included it. Each is replaced with "?"."""
    if isinstance(value, str):
        if value.isascii():
            return value
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _draft(kind: str, sev: str, text: str, data: dict | None = None,
           ts: Any = None) -> dict:
    return {"kind": kind, "sev": sev, "text": text, "data": data or {}, "ts": ts}


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if value is None:
        return "unset"
    if value == "":
        return '""'
    return str(value)


def _label(item: dict) -> str:
    name, norad = item.get("name") or "", item.get("norad")
    if name and norad is not None:
        return f"{name} ({norad})"
    return str(name or norad or "?")


def ua_family(user_agent: str) -> str:
    """The browser family only. The full string identifies a machine more
    precisely than an audit line needs, and changes with every update."""
    ua = user_agent or ""
    if "Edg/" in ua or "EdgA/" in ua or "EdgiOS/" in ua:
        return "Edge"
    if "Firefox/" in ua or "FxiOS/" in ua:
        return "Firefox"
    if "Chrome/" in ua or "CriOS/" in ua or "Chromium/" in ua:
        return "Chrome"
    if "Safari/" in ua:
        return "Safari"
    if ua.lower().startswith("curl/"):
        return "curl"
    return "other"


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

def _strip_url(url: str) -> str:
    """Drop the userinfo, the query and the fragment: the three places a URL
    carries credentials."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url>"
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _plain(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _URL.sub(lambda m: _strip_url(m.group(0)), value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


def _is_secret(name: str, value: Any) -> bool:
    return bool(SECRET_NAME.search(name)) or type(value).__name__ in ("SecretStr", "SecretBytes")


def redact_settings(settings: Settings) -> dict:
    """The settings as the boot record keeps them.

    A secret is recorded only as `<set>` or `<unset>`: enough to see in the
    history that a token was added or removed, never what it was. Any other
    URL loses its userinfo and query string, which is where credentials hide
    in a setting nobody thought of as secret.
    """
    out: dict[str, Any] = {}
    for name in sorted(type(settings).model_fields):
        value = getattr(settings, name, None)
        if _is_secret(name, value):
            out[name] = "<set>" if value not in (None, "", b"") else "<unset>"
        else:
            out[name] = _plain(value)
    return out


def _secret_values(settings: Settings) -> list[str]:
    """The secret values themselves, so text from elsewhere — a logged
    exception, a request — can be scrubbed of them before it is written."""
    out = []
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        if not _is_secret(name, value):
            continue
        getter = getattr(value, "get_secret_value", None)
        raw = getter() if callable(getter) else value
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str) and len(raw) >= 4:
            out.append(raw)
    return sorted(out, key=len, reverse=True)


def diff_settings(old: dict, new: dict) -> list[tuple[str, Any, Any]]:
    """(name, before, after) for every setting that changed between two boots."""
    out = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before != after:
            out.append((name, before, after))
    return out


# --------------------------------------------------------------------------
# derivation: pure functions over consecutive frames
# --------------------------------------------------------------------------

def derive_control(prev: dict | None, cur: dict, journal: Iterable[dict], *,
                   autopilot_engaged: bool = False) -> list[dict]:
    """What changed between two control frames.

    `journal` is ControlService.journal_since(prev's command_seq); only the
    entries this frame accounts for are used, so a command accepted after it
    was published is left for the next frame.

    The lease is read from `armed` and the journal together. armed going
    false is a release only if a release was journaled; otherwise it lapsed.
    An expiry nobody wrote down was the original gap this log exists to fill.
    """
    out: list[dict] = []
    prev_seq = int(prev.get("command_seq") or 0) if prev else 0
    cur_seq = int(cur.get("command_seq") or 0)
    entries = [e for e in journal if prev_seq < int(e.get("seq", 0)) <= cur_seq]
    released = any(e.get("what") == "release" for e in entries)
    was_armed = bool(prev and prev.get("armed"))
    armed = bool(cur.get("armed"))

    # First, because it is the cause of what may follow it: a lapse that
    # catches a slew journals a "lease" stop of its own.
    if was_armed and not armed and not released:
        expired_at = prev.get("lease_expires_at")
        if autopilot_engaged:
            out.append(_draft("control.lease", "warn",
                              "lease expired while autopilot was engaged",
                              {"expired_at": expired_at}, ts=expired_at))
        else:
            out.append(_draft("control.lease", "info", "lease expired",
                              {"expired_at": expired_at}, ts=expired_at))

    missing = (cur_seq - prev_seq) - len(entries)
    if missing > 0:
        out.append(_draft("control.cmd", "warn",
                          f"{missing} command(s) not recorded — the journal had "
                          "moved on before the log read it",
                          {"from_seq": prev_seq, "to_seq": cur_seq}))

    for entry in entries:
        origin = entry.get("origin") or "?"
        what = entry.get("what") or "command"
        sev = "info"
        if origin == "lease":
            # A stop nobody pressed: the operator's own move was cut short.
            sev = "warn"
        elif origin == "operator" and what in ("stop", "release") and autopilot_engaged:
            sev = "warn"
        out.append(_draft("control.cmd", sev, f"{origin}: {what}",
                          {"seq": entry.get("seq"), "origin": origin, "what": what},
                          ts=entry.get("at")))

    expires = cur.get("lease_expires_at")
    if armed and (not was_armed or released):
        # Only an operator can arm. Autopilot never takes a lease.
        out.append(_draft("control.lease", "info",
                          f"operator armed until {_hms(expires)}",
                          {"expires_at": expires}))
    elif armed and was_armed:
        before, after = _parse_ts(prev.get("lease_expires_at")), _parse_ts(expires)
        if before is not None and after is not None and after > before:
            out.append(_draft("control.lease", "info",
                              f"lease extended to {_hms(expires)}",
                              {"expires_at": expires,
                               "was": prev.get("lease_expires_at")}))

    if prev is not None:
        before_gates, after_gates = prev.get("gates") or {}, cur.get("gates") or {}
        for gate, is_open in after_gates.items():
            # The lease lines above already say this one, and better.
            if gate == "armed" or gate not in before_gates:
                continue
            if bool(before_gates[gate]) != bool(is_open):
                out.append(_draft("control.gate", "info",
                                  f"{gate} {'opened' if is_open else 'closed'}",
                                  {"gate": gate, "open": bool(is_open),
                                   "blocked_by": list(cur.get("blocked_by") or [])}))

        reason = cur.get("track_end_reason") or ""
        if reason and (reason != (prev.get("track_end_reason") or "")
                       or cur.get("track_id") != prev.get("track_id")):
            norad = prev.get("target_norad") or cur.get("target_norad")
            what = f"track of {norad}" if norad else "track"
            out.append(_draft("control.track_end", "warn", f"{what} ended — {reason}",
                              {"track_id": cur.get("track_id"), "norad": norad,
                               "reason": reason}))
    return out


def derive_autopilot(prev: dict | None, cur: dict) -> list[dict]:
    """Engaging, standing down, and each change of phase.

    A phase is logged when it changes, not when its detail does. "blocked"
    rewrites its detail as the gates move, and "waiting" as the next pass
    does; one line each until the phase itself changes is the readable
    amount.
    """
    if prev is None:
        return []           # the executor's own "off" at start: a baseline, not news
    was, now = bool(prev.get("enabled")), bool(cur.get("enabled"))
    if now and not was:
        return [_draft("autopilot.engaged", "info", "autopilot engaged",
                       {"phase": cur.get("phase"), "detail": cur.get("detail")})]
    if was and not now:
        because = cur.get("disengaged_because") or cur.get("detail") or ""
        sev = ("info" if because == OPERATOR_OFF
               else "bad" if because.startswith("internal error")
               else "warn")
        text = f"autopilot disengaged — {because}" if because else "autopilot disengaged"
        return [_draft("autopilot.disengaged", sev, text, {"because": because})]
    if now and cur.get("phase") != prev.get("phase"):
        phase, detail = cur.get("phase") or "?", cur.get("detail") or ""
        text = f"autopilot {phase}" + (f": {detail}" if detail else "")
        return [_draft("autopilot.phase", "warn" if phase == "blocked" else "info",
                       text, {"phase": phase, "detail": detail,
                              "current": cur.get("current")})]
    return []


def derive_status(prev_state: str | None, cur: dict) -> list[dict]:
    """A component changing state. Scheduler.set_state already publishes only
    changes, so this mostly guards the first sighting: a component coming up
    healthy at boot is not news, one coming up broken is."""
    component, state = cur.get("component"), cur.get("state")
    if not component or component == "backend" or state == prev_state:
        return []
    if prev_state is None and state == "ok":
        return []
    detail = cur.get("detail") or ""
    sev = {"ok": "info", "degraded": "warn", "down": "bad"}.get(state, "warn")
    text = f"{component} {state}" + (f" — {detail}" if detail else "")
    return [_draft(f"status.{component}", sev, text,
                   {"component": component, "state": state, "was": prev_state,
                    "detail": detail})]


def satnogs_names(frame: dict) -> dict[Any, str]:
    """Job id -> satellite name, from whatever one SatNOGS frame carries.

    Both the jobs list and the observation feed carry the element set's line
    0, "0 KNACKSAT-2", whose "0 " is not part of the name."""
    names: dict[Any, str] = {}
    for obs in frame.get("observations") or []:
        if obs.get("name"):
            names[obs.get("id")] = str(obs["name"]).removeprefix("0 ").strip()
    for job in frame.get("jobs") or []:
        if job.get("tle0"):
            names[job.get("id")] = str(job["tle0"]).removeprefix("0 ").strip()
    return names


def derive_satnogs(prev: dict | None, cur: dict,
                   known: dict[Any, str] | None = None) -> list[dict]:
    """The station's own status, and recordings starting and ending.

    A recording is dated by its own window, not by when this noticed it:
    SatNOGS's jobs list drops an observation the moment it starts and the
    observation feed is polled every two minutes, so "seen running" can be a
    couple of minutes after the fact.

    `known` is names remembered from earlier frames. A running job carries no
    name, and by the time it ends it has been out of the jobs list for the
    whole pass, so without them the end of a recording is a bare number.
    """
    if prev is None:
        return []
    out: list[dict] = []
    before, after = prev.get("station"), cur.get("station")
    if before and after:
        changes = []
        if bool(before.get("is_connected")) != bool(after.get("is_connected")):
            changes.append("satnogs-client connected" if after.get("is_connected")
                           else "satnogs-client disconnected")
        if before.get("status") != after.get("status"):
            changes.append(f"status {before.get('status')} → {after.get('status')}")
        if changes:
            out.append(_draft("satnogs.station", "info",
                              f"station {after.get('id')}: " + ", ".join(changes),
                              {"status": after.get("status"),
                               "is_connected": after.get("is_connected"),
                               "last_seen": after.get("last_seen")}))

    # Names from both frames as well: a job leaves the jobs list at the moment
    # it starts, which is exactly when it is first seen running.
    names: dict[Any, str] = {**(known or {}), **satnogs_names(prev), **satnogs_names(cur)}

    was_running = {r.get("id"): r for r in prev.get("running") or []}
    running = {r.get("id"): r for r in cur.get("running") or []}
    for job_id in sorted(running.keys() - was_running.keys(), key=str):
        job = running[job_id]
        label = _label({"name": names.get(job_id), "norad": job.get("norad")})
        out.append(_draft("satnogs.job_started", "info",
                          f"SatNOGS started recording {label} — job {job_id}",
                          {"job_id": job_id, "norad": job.get("norad"),
                           "start": job.get("start"), "end": job.get("end")},
                          ts=job.get("start")))
    for job_id in sorted(was_running.keys() - running.keys(), key=str):
        job = was_running[job_id]
        label = _label({"name": names.get(job_id), "norad": job.get("norad")})
        end = _parse_ts(job.get("end"))
        out.append(_draft("satnogs.job_ended", "info",
                          f"SatNOGS finished recording {label} — job {job_id}",
                          {"job_id": job_id, "norad": job.get("norad"),
                           "start": job.get("start"), "end": job.get("end")},
                          ts=job.get("end") if end is not None else None))
    return out


def _window(snapshot: dict, lo: datetime, hi: datetime) -> list[tuple[dict, datetime, datetime]]:
    out = []
    for item in snapshot.get("planned") or []:
        aos, los = _parse_ts(item.get("aos")), _parse_ts(item.get("los"))
        if aos is not None and los is not None and los > lo and aos < hi:
            out.append((item, aos, los))
    return out


def _same_pass(a: tuple, b: tuple) -> bool:
    """The same physical pass: same satellite, overlapping in time — exactly
    PlanExecutor._same_pass. A rebuild refines AOS by fractions of a second
    and the key is built from AOS to the second, so the same pass flips
    between two keys across rebuilds; on the key, every other rebuild would
    read as one pass lost and another gained."""
    return a[0].get("norad") == b[0].get("norad") and a[1] < b[2] and b[1] < a[2]


def derive_plan(prev: dict | None, cur: dict, now: datetime,
                window_h: float = PLAN_WINDOW_H) -> list[dict]:
    """One line per rebuild that changed what will be worked in the next hours.

    Both plans are cut to the same window, measured from now, so a pass that
    merely moves inside the window as time passes is not "gained", and one
    that ends is not "lost".
    """
    if not prev or not prev.get("built_at") or not cur.get("built_at"):
        return []
    lo, hi = now, now + timedelta(hours=window_h)
    before, after = _window(prev, lo, hi), _window(cur, lo, hi)
    gained = [a for a in after if not any(_same_pass(a, b) for b in before)]
    lost = [b for b in before if not any(_same_pass(b, a) for a in after)]
    if not gained and not lost:
        return []

    decided = []
    for d in cur.get("decisions") or []:
        aos, los = _parse_ts(d.get("aos")), _parse_ts(d.get("los"))
        if aos is not None and los is not None:
            decided.append((d, aos, los))

    def why(item: tuple) -> tuple[str, str]:
        for d in decided:
            if _same_pass(item, d):
                return d[0].get("status") or "?", d[0].get("reason") or ""
        return "gone", "no longer predicted"

    parts, gained_json, lost_json = [], [], []
    for item, aos, los in gained:
        parts.append(f"+{item.get('name') or item.get('norad')} {_hm(aos)}")
        gained_json.append({"norad": item.get("norad"), "name": item.get("name"),
                            "aos": item.get("aos"), "los": item.get("los"),
                            "max_el": item.get("max_el")})
    for entry in lost:
        item = entry[0]
        status, reason = why(entry)
        parts.append(f"−{item.get('name') or item.get('norad')} {_hm(entry[1])} ({status})")
        lost_json.append({"norad": item.get("norad"), "name": item.get("name"),
                          "aos": item.get("aos"), "los": item.get("los"),
                          "max_el": item.get("max_el"), "status": status,
                          "reason": reason})
    return [_draft("plan.change", "info", "plan: " + ", ".join(parts),
                   {"gained": gained_json, "lost": lost_json,
                    "built_at": cur.get("built_at"), "window_h": window_h})]


# --------------------------------------------------------------------------
# warnings from the backend's own loggers
# --------------------------------------------------------------------------

class WarningCapture(logging.Handler):
    """Hands WARNING and above from the `app` loggers to the running log.

    One module-level instance, attached once in main.py, forwarding to
    whichever EventLog is started. Attaching a handler per lifespan would
    stack one more on every restart of the app inside one process — which is
    what a test suite does.

    A logging handler must never raise into the code that logged, and must
    never log from inside itself: this module's own logger is skipped, and a
    re-entrant call is dropped.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.sink: EventLog | None = None
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        sink = self.sink
        if sink is None or record.name == __name__:
            return
        if getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            sink.capture(record)
        except Exception:
            pass
        finally:
            self._local.busy = False


warning_capture = WarningCapture()


# --------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------

class EventLog:
    """Derives, keeps and serves the station logbook. See the module docstring."""

    def __init__(self, settings: Settings, control=None, executor=None, satnogs=None,
                 planner=None, on_state=None, *, version: str = "",
                 clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        self.s = settings
        self.control = control
        self.executor = executor
        self.satnogs = satnogs
        self.planner = planner
        self.on_state = on_state or (lambda component, state, detail="": None)
        self.version = version
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self.dir = Path(settings.data_dir) / "events"

        self._ring: deque[dict] = deque(maxlen=RING_SIZE)
        # Whether the ring holds every record on disk, so a query reaching
        # past its oldest entry need not read the files.
        self._loaded_all = True
        self._counter = 0
        self._unwritten: list[dict] = []
        self._io_lock = threading.Lock()
        self._wake = asyncio.Event()
        self._disk_ok = True
        self._retry_at = 0.0
        self._retention_day: str | None = None
        self._secrets = _secret_values(settings)
        self._repeats: dict[tuple[str, str], dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._started_at = self._monotonic()
        self._closed = False
        self._runs = 0

        # The last frame of each type seen, and what was derived from them.
        self._last: dict[str, dict] = {}
        self._components: dict[str, str] = {}
        self._engaged = False
        self._job_names: dict[Any, str] = {}

        # Subscribed from construction, which main.py does before the
        # scheduler starts — so the frames published while it starts are
        # already queued here when run() first reads.
        self._conn = hub.register()
        self._conn.topics = set(TOPICS)

    # --- recording ----------------------------------------------------------
    def emit(self, kind: str, sev: str, text: str, data: dict | None = None, *,
             ts: Any = None) -> dict:
        """Record one event: into the ring, onto the disk queue, out as a
        `log` frame. Call on the event loop.

        The id is taken from the time of recording and a counter, so ids sort
        in the order records were made. `ts` is when the thing happened —
        a journal entry's acceptance, a lease's expiry — which can be a little
        earlier; it is never later, and never more than BACKDATE_MAX earlier.
        """
        now = self._clock()
        self._counter += 1
        data = _clean(data or {})
        when = _parse_ts(ts)
        if when is None or when > now:
            when = now
        elif now - when > BACKDATE_MAX:
            # Noticed long after the fact. Dated by the noticing, so the
            # record is where a reader paging by id will look for it.
            data["happened_at"] = _iso(when)
            when = now
        record = {
            "id": f"{int(now.timestamp() * 1000)}-{self._counter}",
            "ts": _iso(when),
            "kind": _clean(kind),
            "sev": sev if sev in SEVERITY else "info",
            "text": _clean(text),
            "data": data,
        }
        self._ring.append(record)
        self._unwritten.append(record)
        self._wake.set()
        try:
            hub.publish("log", record)
        except Exception:
            log.debug("could not publish a log frame", exc_info=True)
        return record

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "<redacted>")
        return text

    # --- deriving -----------------------------------------------------------
    def _journal_since(self, seq: int) -> list[dict]:
        since = getattr(self.control, "journal_since", None)
        if not callable(since):
            return []
        try:
            return list(since(seq))
        except Exception:
            log.exception("could not read the command journal")
            return []

    def ingest(self, frame) -> None:
        """Derive events from one hub frame and record them."""
        kind, data, ts = frame.type, frame.data or {}, frame.ts
        if kind == "control":
            prev = self._last.get("control")
            journal = self._journal_since(int(prev.get("command_seq") or 0) if prev else 0)
            drafts = derive_control(prev, data, journal, autopilot_engaged=self._engaged)
        elif kind == "autopilot":
            drafts = derive_autopilot(self._last.get("autopilot"), data)
            self._engaged = bool(data.get("enabled"))
        elif kind == "status":
            component = data.get("component")
            drafts = derive_status(self._components.get(component), data)
            if component:
                self._components[component] = data.get("state")
        elif kind == "satnogs":
            drafts = derive_satnogs(self._last.get("satnogs"), data, self._job_names)
            self._job_names.update(satnogs_names(data))
            for old in list(self._job_names)[:-JOB_NAMES_KEPT]:
                self._job_names.pop(old, None)
        elif kind == "plan":
            drafts = derive_plan(self._last.get("plan"), data,
                                 _parse_ts(ts) or self._clock())
        else:
            return
        if kind != "status":
            self._last[kind] = data
        for d in drafts:
            self.emit(d["kind"], d["sev"], d["text"], d["data"], ts=d["ts"] or ts)

    def pump(self) -> int:
        """Derive from every frame already queued, without waiting. Returns
        how many were read. The loop calls it; so do tests and close()."""
        count = 0
        while True:
            try:
                frame = self._conn.queue.get_nowait()
            except asyncio.QueueEmpty:
                return count
            count += 1
            try:
                self.ingest(frame)
            except Exception as exc:
                # A derivation bug costs a line, not the log.
                log.exception("logbook could not read a %s frame", frame.type)
                self.emit("log.error", "bad",
                          f"logbook could not read a {frame.type} frame: {exc}")

    # --- warnings -----------------------------------------------------------
    def capture(self, record: logging.LogRecord) -> None:
        """Called by WarningCapture, on whatever thread logged."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._closed:
            return
        exc = ""
        if record.exc_info and record.exc_info[1] is not None:
            exc = "".join(traceback.format_exception_only(
                record.exc_info[0], record.exc_info[1])).strip()
        # Formatted here, on the caller's thread, while its arguments are
        # still what they were when it logged.
        item = (record.name, str(record.msg), record.levelno,
                record.getMessage(), exc)
        if threading.get_ident() == self._loop_thread:
            self._on_warning(*item)
        else:
            # A worker thread: the planner's compute and the waterfall crop
            # both log from one. Recording touches the ring and the hub,
            # which belong to the loop.
            loop.call_soon_threadsafe(self._on_warning, *item)

    def _on_warning(self, name: str, template: str, level: int, message: str,
                    exc: str) -> None:
        if (name, template) in DERIVED_ELSEWHERE or self._closed:
            return
        key = (name, template)
        now = self._monotonic()
        seen = self._repeats.get(key)
        if seen is not None and now - seen["first"] < REPEAT_WINDOW_S:
            # A SatNOGS outage logs the same failure every poll. One line,
            # then a count when the window closes.
            seen["count"] += 1
            seen["last"] = self._scrub(message)
            return
        if seen is not None:
            self._summarise_repeats(key, seen)
        self._repeats[key] = {"first": now, "count": 0, "level": level,
                              "name": name, "last": ""}
        error = level >= logging.ERROR
        short = name.rsplit(".", 1)[-1]
        data = {"logger": name, "level": logging.getLevelName(level)}
        if exc:
            data["exc"] = self._scrub(exc)
        self.emit("log.error" if error else "log.warning", "bad" if error else "warn",
                  f"{short}: {self._scrub(message)}", data)

    def _summarise_repeats(self, key: tuple[str, str], seen: dict) -> None:
        if seen["count"] <= 0:
            return
        error = seen["level"] >= logging.ERROR
        short = seen["name"].rsplit(".", 1)[-1]
        minutes = round(REPEAT_WINDOW_S / 60)
        self.emit("log.error" if error else "log.warning", "bad" if error else "warn",
                  f"{short}: repeated {seen['count']} times in {minutes} min — "
                  f"last: {seen['last']}",
                  {"logger": seen["name"], "repeats": seen["count"],
                   "template": key[1]})

    def flush_repeats(self, *, force: bool = False) -> None:
        now = self._monotonic()
        for key, seen in list(self._repeats.items()):
            if force or now - seen["first"] >= REPEAT_WINDOW_S:
                self._summarise_repeats(key, seen)
                self._repeats.pop(key, None)

    # --- requests -----------------------------------------------------------
    def audit(self, scope: dict, status: int, body: bytes, response: bytes, *,
              ts: Any = None) -> dict:
        """One line for a request that could change what the station does.

        `ts` is when the request arrived. The line is written once the reply
        has gone, but dated by arrival, as an access log is: an ARM's lease
        line and a STOP's journal entry are stamped while the request is being
        handled, and the request that caused them should read as coming first.
        """
        headers: dict[str, str] = {}
        for raw_key, raw_value in scope.get("headers") or []:
            headers[raw_key.decode("latin-1").lower()] = raw_value.decode("latin-1")
        peer = str((scope.get("client") or ("?",))[0])
        # Caddy sets X-Forwarded-For; without it the peer is the client. The
        # header's first hop is the client as Caddy saw it, so it is what is
        # named — and the peer is kept beside it whenever the two differ.
        forwarded = headers.get("x-forwarded-for", "").split(",")[0].strip()[:64]
        client = forwarded or peer
        method, path = scope.get("method", "?"), scope.get("path", "?")

        fields: dict[str, Any] = {}
        try:
            payload = json.loads(body) if body else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in AUDIT_FIELDS:
                if key not in payload:
                    continue
                value = payload[key]
                if value is None or isinstance(value, (bool, int, float)):
                    fields[key] = value
                elif isinstance(value, str):
                    fields[key] = self._scrub(value[:40])

        blocked: list[str] = []
        reason = ""
        if status == 409 and response:
            try:
                detail = (json.loads(response) or {}).get("detail")
            except (ValueError, UnicodeDecodeError, AttributeError):
                detail = None
            if isinstance(detail, dict):
                blocked = [str(g) for g in detail.get("blocked_by") or []]
                reason = str(detail.get("detail") or detail.get("error") or "")
            elif isinstance(detail, str):
                reason = detail

        text = f"{method} {path} {status}"
        if blocked:
            text += f" blocked_by {', '.join(blocked)}"
        elif reason:
            text += f" refused: {reason}"
        text += f" from {client}"
        sev = "info" if status < 400 else "bad" if status >= 500 else "warn"
        data: dict[str, Any] = {
            "method": method, "path": path, "status": status, "client": client,
            "ua": ua_family(headers.get("user-agent", "")), "fields": fields,
            "blocked_by": blocked,
        }
        if forwarded and peer != forwarded:
            data["peer"] = peer
        if reason:
            data["reason"] = self._scrub(reason)
        return self.emit("audit", sev, self._scrub(text), data, ts=ts)

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        """Load the history, then record the boot and what it says about the
        run before it. Call once, before the scheduler starts."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        warning_capture.sink = self
        # Cleaned here as well as in emit(): last_boot.json is written from it.
        snapshot = _clean(redact_settings(self.s))

        history: list[dict] = []
        loaded_all, previous, last_autopilot = True, None, None
        try:
            history, loaded_all, previous, last_autopilot = await asyncio.to_thread(self._load)
        except OSError as exc:
            self._degrade(exc)
        early = list(self._ring)
        self._ring = deque(history + early, maxlen=RING_SIZE)
        self._loaded_all = loaded_all

        mock = bool(getattr(self.s, "mock", False))
        self.emit("boot", "info",
                  f"backend started — version {self.version or '?'}, "
                  f"{'MOCK hardware' if mock else 'real hardware'}, pid {os.getpid()}",
                  {"version": self.version, "mock": mock, "pid": os.getpid(),
                   "settings": snapshot})
        last = history[-1] if history else None
        if last is not None and last.get("kind") != "shutdown":
            self.emit("unclean_restart", "warn",
                      "the previous run ended without a shutdown record — last "
                      f"record at {_hms(last.get('ts'))} ({last.get('kind')})",
                      {"last_seen": last.get("ts"), "last_kind": last.get("kind"),
                       "last_id": last.get("id")})
        # The newest boot, engage or disengage on disk: only an engage means
        # the run before this one ended with autopilot on.
        if last_autopilot is not None and last_autopilot.get("kind") == "autopilot.engaged":
            # Said, never acted on. The executor always starts off, and that
            # is right: the lease it ran under died with the old process.
            self.emit("autopilot.disengaged", "warn",
                      "backend restarted — autopilot was engaged and is now off; "
                      "re-engaging is a human decision",
                      {"because": "backend restarted",
                       "engaged_at": last_autopilot.get("ts")})
        if previous is not None:
            for name, before, after in diff_settings(previous, snapshot):
                self.emit("config_change",
                          "warn" if name in SAFETY_SETTINGS else "info",
                          f"{name} {_fmt(before)} → {_fmt(after)}",
                          {"name": name, "before": before, "after": after})
        try:
            await asyncio.to_thread(self._write_last_boot, snapshot)
        except OSError as exc:
            self._degrade(exc)
        await self._flush(force=True)
        await self._retention()

    async def run(self) -> None:
        """Read frames as they come, derive, and write. Supervised by the
        scheduler like every other loop."""
        if self._loop is None:
            await self.start()
        self._runs += 1
        if self._runs > 1 and self._disk_ok:
            # Restarted after a crash, which the scheduler reported as down.
            # Nothing else would ever say it is back.
            self.on_state("events", "ok", "running again")
        while True:
            frame = await self._next_frame(HOUSEKEEPING_S)
            if frame is not None:
                try:
                    self.ingest(frame)
                except Exception as exc:
                    log.exception("logbook could not read a %s frame", frame.type)
                    self.emit("log.error", "bad",
                              f"logbook could not read a {frame.type} frame: {exc}")
            self.pump()
            self.flush_repeats()
            if self._clock().date().isoformat() != self._retention_day:
                await self._retention()
            await self._flush()

    async def _next_frame(self, timeout: float):
        """The next queued frame, or None once something else needs doing:
        a record emitted from outside the frames (a request, a warning)
        waiting to be written, or housekeeping falling due."""
        try:
            return self._conn.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        if self._unwritten:
            return None
        self._wake.clear()
        getter = asyncio.ensure_future(self._conn.queue.get())
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            done, _ = await asyncio.wait({getter, waker}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (getter, waker):
                if not task.done():
                    task.cancel()
        if getter in done and not getter.cancelled():
            return getter.result()
        return None

    async def close(self) -> None:
        """Read what is left, write the shutdown record, let go of the hub.

        The shutdown record is what tells the next boot this one ended on
        purpose; its absence is how an unclean restart is recognised.
        """
        if self._closed:
            return
        try:
            self.pump()
            self.flush_repeats(force=True)
            self.emit("shutdown", "info", "backend stopped",
                      {"uptime_s": round(self._monotonic() - self._started_at)})
            await self._flush(force=True)
        finally:
            self._closed = True
            self.detach()

    def detach(self) -> None:
        """Stop receiving frames and warnings, without writing anything."""
        hub.unregister(self._conn)
        if warning_capture.sink is self:
            warning_capture.sink = None

    # --- disk ---------------------------------------------------------------
    def _degrade(self, exc: BaseException) -> None:
        detail = f"logbook kept in memory only: {exc}"
        self._retry_at = self._monotonic() + DISK_RETRY_S
        if self._disk_ok:
            log.warning("cannot write the station logbook to %s: %s", self.dir, exc)
        self._disk_ok = False
        self.on_state("events", "degraded", detail)

    async def _flush(self, *, force: bool = False) -> None:
        if not self._unwritten:
            return
        batch, self._unwritten = self._unwritten, []
        if not self._disk_ok and not force and self._monotonic() < self._retry_at:
            return          # degraded: these stay in the ring only
        try:
            await asyncio.to_thread(self._write, batch)
        except OSError as exc:
            self._degrade(exc)
            return
        if not self._disk_ok:
            self._disk_ok = True
            self.on_state("events", "ok", "writing to disk again")

    def _day_path(self, day: str) -> Path:
        return self.dir / f"{day}.jsonl"

    def _write(self, batch: list[dict]) -> None:
        """Append records to their UTC days' files. Runs in a worker thread."""
        with self._io_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            by_day: dict[str, list[dict]] = {}
            for record in batch:
                by_day.setdefault(record["ts"][:10], []).append(record)
            for day, records in by_day.items():
                path = self._day_path(day)
                torn = False
                if path.exists() and path.stat().st_size > 0:
                    with open(path, "rb") as fh:
                        fh.seek(-1, os.SEEK_END)
                        torn = fh.read(1) != b"\n"
                durable = any(r["kind"].startswith("control.")
                              or r["kind"] in ("boot", "shutdown") for r in records)
                with open(path, "ab") as fh:
                    if torn:
                        # A power cut mid-line. Start on a fresh line, or the
                        # next record would be glued to the torn one and be
                        # lost with it.
                        fh.write(b"\n")
                    for record in records:
                        fh.write(self._line(record))
                        fh.flush()
                    if durable:
                        os.fsync(fh.fileno())

    @staticmethod
    def _line(record: dict) -> bytes:
        """One record as one line of UTF-8.

        emit() has already made its text encodable. This does not rely on it:
        a record that cannot be written costs its own line and nothing more —
        not the batch around it, and not the writer task, which only expects
        the disk to fail."""
        try:
            text = json.dumps(record, ensure_ascii=False, default=str,
                              separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            text = json.dumps({"id": record.get("id"), "ts": record.get("ts"),
                               "kind": "log.error", "sev": "bad",
                               "text": f"logbook could not write a "
                                       f"{record.get('kind')} record: {exc}",
                               "data": {}},
                              ensure_ascii=False, default=str, separators=(",", ":"))
        return text.encode("utf-8", "replace") + b"\n"

    def _write_last_boot(self, snapshot: dict) -> None:
        with self._io_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "last_boot.json.tmp"
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1,
                                      sort_keys=True, default=str),
                           encoding="utf-8", errors="replace")
            os.replace(tmp, self.dir / "last_boot.json")

    def _day_files(self) -> list[tuple[str, Path, int]]:
        """(day, path, bytes) for every day file, oldest first."""
        if not self.dir.is_dir():
            if self.dir.exists():
                raise NotADirectoryError(f"{self.dir} is not a directory")
            return []
        out = []
        for path in self.dir.iterdir():
            match = _DAY_FILE.match(path.name)
            if match and path.is_file():
                out.append((match.group(1), path, path.stat().st_size))
        return sorted(out)

    @staticmethod
    def _read_file(path: Path) -> list[dict]:
        """A day file's records. A line that does not parse — the torn tail
        of a write a power cut interrupted — is skipped, not fatal."""
        out = []
        with open(path, "rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict) and "id" in record and "kind" in record:
                    out.append(record)
        return out

    @staticmethod
    def _scan_markers(path: Path) -> list[dict]:
        """Only a day file's boot, engage and disengage records. Every line is
        looked at but only the few that can be one are parsed, and nothing
        else is kept."""
        out = []
        with open(path, "rb") as fh:
            for raw in fh:
                if b"autopilot." not in raw and b"boot" not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (isinstance(record, dict) and "id" in record
                        and record.get("kind") in AUTOPILOT_MARKERS):
                    out.append(record)
        return out

    def _load(self) -> tuple[list[dict], bool, dict | None, dict | None]:
        """The newest RING_SIZE records, whether that was all of them, the
        previous boot's settings, and the newest boot, engage or disengage.
        Runs in a worker thread.

        It reads back only as far as those need, because start() holds up
        the API and every loop until it is done. The ring is complete once
        no older file can hold one of its records. Autopilot's state is
        settled by the newest boot, engage or disengage: every run starts
        with a boot record and autopilot off, so nothing before a run's boot
        can say what that run did. A station that has never engaged autopilot
        — 5024 today — finds its answer in the newest file, rather than
        parsing every retained day for a record that is not there.
        """
        with self._io_lock:
            files = self._day_files()
            records: list[dict] = []
            complete = True         # every record on disk is in `records`
            last_autopilot: dict | None = None
            for day, path, _ in reversed(files):
                ring_done = False
                if len(records) >= RING_SIZE:
                    records.sort(key=_by_id, reverse=True)
                    if len(records) > RING_SIZE:
                        complete = False
                        del records[RING_SIZE:]
                    ring_done = not _may_hold_after(day, _by_id(records[-1]))
                settled = (last_autopilot is not None
                           and not _may_hold_after(day, _by_id(last_autopilot)))
                if ring_done:
                    complete = False
                    if settled:
                        break
                    found = self._scan_markers(path)
                else:
                    found = self._read_file(path)
                    records.extend(found)
                for record in found:
                    if record.get("kind") in AUTOPILOT_MARKERS and (
                            last_autopilot is None
                            or _by_id(record) > _by_id(last_autopilot)):
                        last_autopilot = record
            records.sort(key=_by_id)
            if len(records) > RING_SIZE:
                complete = False
                records = records[-RING_SIZE:]
            loaded_all = complete

            previous = None
            boot_file = self.dir / "last_boot.json"
            if boot_file.is_file():
                try:
                    previous = json.loads(boot_file.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    previous = None
                if not isinstance(previous, dict):
                    previous = None
            return records, loaded_all, previous, last_autopilot

    async def _retention(self) -> None:
        today = self._clock().date().isoformat()
        self._retention_day = today
        if not self._disk_ok:
            return
        try:
            removed = await asyncio.to_thread(self._apply_retention, today)
        except OSError as exc:
            log.warning("logbook retention failed: %s", exc)
            return
        if removed:
            self.emit("retention", "info",
                      f"logbook retention removed {len(removed)} day file(s), "
                      f"{removed[0]} to {removed[-1]}",
                      {"removed": removed})

    def _apply_retention(self, today: str) -> list[str]:
        """Age first, then size. Today's file is never removed: it is the one
        being written, and the record of why it is so large."""
        with self._io_lock:
            files = self._day_files()
            cutoff = (date.fromisoformat(today)
                      - timedelta(days=int(self.s.events_retain_days))).isoformat()
            removed: list[str] = []
            kept = []
            for day, path, size in files:
                if day < cutoff and day != today:
                    path.unlink(missing_ok=True)
                    removed.append(day)
                else:
                    kept.append((day, path, size))
            cap = float(self.s.events_max_mb) * 1024 * 1024
            total = sum(size for _, _, size in kept)
            for day, path, size in kept:
                if total <= cap or day == today:
                    break
                path.unlink(missing_ok=True)
                removed.append(day)
                total -= size
            return sorted(removed)

    # --- reading ------------------------------------------------------------
    async def days(self) -> list[dict]:
        def listing() -> list[dict]:
            with self._io_lock:
                try:
                    files = self._day_files()
                except OSError:
                    return []
                return [{"day": day, "bytes": size} for day, _, size in reversed(files)]
        return await asyncio.to_thread(listing)

    async def read_day(self, day: str) -> bytes | None:
        """A whole day file, read under the writer's lock so a half-written
        line is never served. None if there is no such day."""
        if not _DAY.match(day):
            raise ValueError(f"not a day: {day!r}")
        date.fromisoformat(day)          # 2026-02-30 is not a day either

        def read() -> bytes | None:
            with self._io_lock:
                path = self._day_path(day)
                return path.read_bytes() if path.is_file() else None
        return await asyncio.to_thread(read)

    @staticmethod
    def _bound(raw: str | None) -> tuple[str, Any] | None:
        """A since/until bound: an event id, or an ISO time.

        Either must be a time the calendar can do arithmetic on: an id from
        past the year 9999, or 0001-01-01 in a zone east of UTC, is refused
        here rather than overflowing later as a 500."""
        if raw is None or raw == "":
            return None
        try:
            if _ID.match(raw):
                key = id_key(raw)
                _id_time(key)           # raises for an id from no real time
                return ("id", key)
            ts = _parse_ts(raw)
            if ts is not None:
                return ("ts", _iso(ts))
        except (OverflowError, OSError, ValueError):
            pass
        raise ValueError(f"not an event id or an ISO time: {raw!r}")

    @staticmethod
    def _after(record: dict, bound: tuple[str, Any] | None) -> bool:
        if bound is None:
            return True
        if bound[0] == "id":
            return id_key(record.get("id")) > bound[1]
        return str(record.get("ts", "")) >= bound[1]

    @staticmethod
    def _before(record: dict, bound: tuple[str, Any] | None) -> bool:
        if bound is None:
            return True
        if bound[0] == "id":
            return id_key(record.get("id")) < bound[1]
        return str(record.get("ts", "")) < bound[1]

    async def query(self, *, since: str | None = None, until: str | None = None,
                    kinds: str | None = None, min_sev: str | None = None,
                    q: str | None = None, limit: int = 200) -> tuple[list[dict], bool]:
        """Records newest first, and whether there are more past the limit.

        Served from the ring when it reaches back far enough, which for the
        live page is always. Only a query older than the ring reads the day
        files, and then only as many as it needs.
        """
        limit = max(1, min(int(limit), QUERY_MAX))
        lo, hi = self._bound(since), self._bound(until)
        if min_sev is not None and min_sev not in SEVERITY:
            raise ValueError(f"min_sev must be one of {', '.join(SEVERITY)}")
        floor = SEVERITY.get(min_sev or "info", 0)
        prefixes = [k.strip() for k in (kinds or "").split(",") if k.strip()]
        needle = (q or "").strip().lower()

        def matches(record: dict) -> bool:
            if SEVERITY.get(record.get("sev"), 0) < floor:
                return False
            kind = str(record.get("kind", ""))
            if prefixes and not any(kind == p or kind.startswith(p + ".")
                                    for p in prefixes):
                return False
            if needle and needle not in str(record.get("text", "")).lower() \
                    and needle not in kind.lower():
                return False
            return self._after(record, lo) and self._before(record, hi)

        ring = list(self._ring)
        hits = [r for r in reversed(ring) if matches(r)]
        oldest = ring[0] if ring else None
        # The ring answers on its own if it holds everything there is, or if
        # nothing older than it can be inside the query's lower bound. For a
        # time bound that is judged by when its oldest record was recorded,
        # not by its ts: a record dated a little early can sit after records
        # dated later than it.
        complete = self._loaded_all and len(ring) < RING_SIZE
        reaches = complete
        if lo is not None and oldest is not None:
            if lo[0] == "id":
                reaches = reaches or not self._after(oldest, lo)
            else:
                reaches = reaches or _iso(_id_time(_by_id(oldest))) < lo[1]
        if len(hits) <= limit and not reaches and oldest is not None:
            older = await asyncio.to_thread(
                self._read_older, id_key(oldest.get("id")), lo, matches,
                limit + 1 - len(hits))
            hits.extend(older)
        return hits[:limit], len(hits) > limit

    def _read_older(self, before: tuple[int, int], lo: tuple[str, Any] | None,
                    matches: Callable[[dict], bool], need: int) -> list[dict]:
        """Matching records older than the ring, newest first. Worker thread.

        A file holds the records dated its day, and ids go by when a record
        was made, which can be up to BACKDATE_MAX later: a recording that
        ended at 23:59 and was noticed at 00:01 is in the old day's file with
        a new day's id. So a full page is not the end of the reading; the
        reading ends when no older file can hold an id above the page's
        lowest.
        """
        first_day = None
        if lo is not None:
            if lo[0] == "ts":
                # A file holds only records dated its own day.
                first_day = lo[1][:10]
            else:
                first_day = (_id_time(lo[1]) - BACKDATE_MAX).date().isoformat()
        # A record is never dated after it was made, so nothing older than
        # the ring is filed after the day its oldest record was made.
        last_day = _id_time(before).date().isoformat()
        out: list[dict] = []
        with self._io_lock:
            try:
                files = self._day_files()
            except OSError:
                return []
            for day, path, _ in reversed(files):
                if day > last_day:
                    continue
                if first_day is not None and day < first_day:
                    break
                if len(out) >= need:
                    out.sort(key=_by_id, reverse=True)
                    if not _may_hold_after(day, _by_id(out[need - 1])):
                        break
                try:
                    found = self._read_file(path)
                except OSError:
                    continue
                out.extend(r for r in found if _by_id(r) < before and matches(r))
        out.sort(key=_by_id, reverse=True)
        return out


# --------------------------------------------------------------------------
# the request audit
# --------------------------------------------------------------------------

class AuditMiddleware:
    """Records every request that can change what the station does.

    Plain ASGI rather than BaseHTTPMiddleware: the request body is copied as
    the route reads it and the response as it is sent, so the route receives
    exactly the messages it would have without this, and nothing is buffered
    or replayed. A refused command's gates are read from its 409 body, which
    is the one response whose content the audit line needs.

    It finds the log on `app.state.events`, so an app built without one — a
    test, a tool — passes through untouched.

    Commands sent over the WebSocket do not pass through here. They still
    appear in the log, through the command journal, but without the client.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        state = getattr(scope.get("app"), "state", None)
        events = getattr(state, "events", None)
        if (events is None
                or scope.get("type") != "http"
                or scope.get("method") not in AUDITED_METHODS
                or not str(scope.get("path", "")).startswith(AUDITED_PREFIXES)):
            await self.app(scope, receive, send)
            return

        arrived = events._clock()
        body = bytearray()
        reply = bytearray()
        status = {"code": 500}

        async def receive_copy():
            message = await receive()
            if message.get("type") == "http.request" and len(body) < BODY_CAP:
                body.extend(message.get("body", b"")[: BODY_CAP - len(body)])
            return message

        async def send_copy(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            elif (message.get("type") == "http.response.body"
                  and status["code"] == 409 and len(reply) < BODY_CAP):
                reply.extend(message.get("body", b"")[: BODY_CAP - len(reply)])
            await send(message)

        try:
            await self.app(scope, receive_copy, send_copy)
        finally:
            try:
                events.audit(scope, status["code"], bytes(body), bytes(reply),
                             ts=arrived)
            except Exception:
                log.exception("could not audit %s %s",
                              scope.get("method"), scope.get("path"))
