"""Settings, assembled from a .env file, the environment, then CLI flags.

This is the only module that reads os.environ. Everything else takes a
Settings instance. Precedence is CLI > environment > .env file > default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# SatNOGS station 5024 reports min_culmination=10, and the Network will not
# schedule below its own limit. We read the real value from the API at run
# time; these are only the fallbacks for when the station omits them.
# Station 5024 publishes min_culmination=10 but does not mark it hard, so a
# CLI value is allowed to go under it - which is how the official tool's `-m 3`
# ends up recording 3 degree grazing passes. 5 degrees is the chosen middle:
# better than 3, still catching low passes the station's own 10 would refuse.
DEFAULT_MIN_CULMINATION_DEG = 5.0
DEFAULT_MIN_HORIZON_DEG = 0.0

# "Balanced" profile.
DEFAULT_HOURS = 48.0
DEFAULT_MIN_DURATION_S = 240.0     # 4 minutes
DEFAULT_MAX_DURATION_S = 1800.0    # 30 minutes; longer passes get clamped
DEFAULT_BUFFER_S = 30.0            # rotator reset between observations
DEFAULT_MAX_SCHEDULE = 20

# KNACKSAT-2. The station's own mission satellite, which outranks everything.
DEFAULT_MISSION_NORAD = 67683

DB_BASE_URL = "https://db.satnogs.org/api"
NETWORK_BASE_URL = "https://network.satnogs.org/api"

# db.satnogs.org/api/tle/ is regenerated from Space-Track a few times a day.
# Two hours is the same floor the SatNOGS tooling uses; do not lower it.
TLE_TTL_S = 7200
TRANSMITTER_TTL_S = 86400
SATELLITE_TTL_S = 86400
HISTORY_TTL_S = 86400


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Missing file is not an error."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass
class Settings:
    station_id: int = 0
    network_token: str = ""
    db_token: str = ""
    cache_dir: Path = field(default_factory=lambda: Path("cache"))

    hours: float = DEFAULT_HOURS
    min_culmination: float | None = None   # None -> take the station's value
    min_horizon: float | None = None       # None -> take the station's value
    min_duration_s: float = DEFAULT_MIN_DURATION_S
    max_duration_s: float = DEFAULT_MAX_DURATION_S
    buffer_s: float = DEFAULT_BUFFER_S
    max_schedule: int = DEFAULT_MAX_SCHEDULE
    mission_norad: int | None = DEFAULT_MISSION_NORAD

    priority_file: Path | None = None
    only_priority: bool = False
    min_priority: float = 0.0
    allow_frequency_violators: bool = False
    exclude: set[int] = field(default_factory=set)
    only: set[int] = field(default_factory=set)
    modes: set[str] = field(default_factory=set)
    services: set[str] = field(default_factory=set)

    offline: bool = False
    execute: bool = False
    assume_yes: bool = False
    verbose: bool = False

    db_base_url: str = DB_BASE_URL
    network_base_url: str = NETWORK_BASE_URL

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        dotenv = load_dotenv(env_file or Path(".env"))

        def pick(name: str, default: str = "") -> str:
            # A real environment variable beats the .env file.
            return os.environ.get(name) or dotenv.get(name, default)

        cache = pick("SATNOGS_CACHE_DIR") or "cache"
        station = pick("SATNOGS_STATION_ID")
        priority_file = pick("SATNOGS_PRIORITY_FILE")
        return cls(
            station_id=int(station) if station.strip().isdigit() else 0,
            network_token=pick("SATNOGS_NETWORK_TOKEN"),
            db_token=pick("SATNOGS_DB_TOKEN"),
            cache_dir=Path(cache),
            priority_file=Path(priority_file) if priority_file else None,
        )

    def merge_args(self, args) -> "Settings":
        """Overlay parsed CLI arguments. Only values actually given win."""
        for name in (
            "hours", "min_culmination", "min_horizon", "min_duration_s",
            "max_duration_s", "buffer_s", "max_schedule", "mission_norad",
            "priority_file", "offline", "execute", "assume_yes", "verbose",
            "only_priority", "min_priority", "allow_frequency_violators",
        ):
            value = getattr(args, name, None)
            if value is not None:
                setattr(self, name, value)

        if getattr(args, "station", None):
            self.station_id = args.station
        if getattr(args, "cache_dir", None):
            self.cache_dir = Path(args.cache_dir)
        for name in ("exclude", "only", "modes", "services"):
            value = getattr(args, name, None)
            if value:
                # Modes and services are compared case-insensitively.
                if name == "modes":
                    value = [str(v).upper() for v in value]
                elif name == "services":
                    value = [str(v).lower() for v in value]
                setattr(self, name, set(value))
        # --no-mission clears the mission satellite entirely.
        if getattr(args, "no_mission", False):
            self.mission_norad = None
        return self
