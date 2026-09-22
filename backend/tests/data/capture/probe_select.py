"""Offline probe: which (norad, uuid) candidates yield passes in the capture window."""
import json, datetime as dt, collections
from auto_scheduler.utils import satellites_by_norad_from_transmitters, search_transmitters
from auto_scheduler.pass_predictor import create_observer, find_constrained_passes
from auto_scheduler.tle import parse_tle0

FX = "/w/upstream_fixtures/"
sats = {int(k): v for k, v in json.load(open(FX + "satellites.json")).items()}
recv = json.load(open(FX + "transmitters_receivable.json"))
stats = json.load(open(FX + "transmitters_stats.json"))
tles = json.load(open(FX + "tles.json"))

transmitters = search_transmitters(recv, stats, sats, {"skip_frequency_violators": True})
by_norad = satellites_by_norad_from_transmitters(transmitters, tles)
print("transmitters", len(transmitters), "satellites", len(by_norad))

tmin = dt.datetime(2023, 2, 18, 9, 0, 0, tzinfo=dt.timezone.utc)
tmax = tmin + dt.timedelta(hours=24)
obs = create_observer(52.0, 4.35, 10, min_riseset=5.0)
constraints = {"time": (tmin, tmax), "pass_duration": (3.0, 30.0), "azimuth": (0.0, 360.0),
               "min_culmination": 3.0, "angular_separation": (None, 0.0, 90.0)}

counts = collections.Counter()
names = {}
for t in transmitters:
    s = by_norad.get(t.norad_cat_id)
    if s is None:
        continue
    p = find_constrained_passes(s, t, obs, constraints)
    if p:
        counts[(t.norad_cat_id, t.uuid, t.mode)] = len(p)
        names[t.norad_cat_id] = parse_tle0(s.orbit.data.line0)

print("pairs with passes:", len(counts))
for (n, u, m), c in counts.most_common(45):
    print(f"{n:6d} {u:24s} {m:12s} passes={c:2d}  tle0={names[n]!r}  db={sats[n]['name']!r}")

print("--- TLE names with parens/spaces ---")
seen = set()
for (n, u, m), c in counts.items():
    nm = names[n]
    if n not in seen and ("(" in nm or " " in nm):
        seen.add(n)
        print(f"{n:6d} {nm!r}")

print("--- catalog satellites that are frequency violators ---")
v = [(n, s["name"], s["status"]) for n, s in sats.items() if s.get("is_frequency_violator")]
print(len(v), v[:8])
