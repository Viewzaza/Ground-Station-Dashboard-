"""Settings.

This is the only module that reads the environment. Everything else receives a
Settings instance. That is what makes GS_MOCK a single switch rather than a flag
tested in twenty places.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv_ints(raw: str) -> list[int]:
    return [int(p) for p in (x.strip() for x in raw.split(",")) if p]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GS_", extra="ignore")

    # --- mode ---------------------------------------------------------------
    mock: bool = True
    offline: bool = False
    log_level: str = "info"
    data_dir: Path = Path("/data")

    # --- station ------------------------------------------------------------
    station_id: int = 5024
    station_name: str = "INSTED-Ground Station(UHF)"
    station_lat: float = 13.823781779271652
    station_lon: float = 100.51295302800892
    station_alt_m: float = 60.0
    station_grid: str = "OK03gt"
    # Station 5024 reports min_horizon=0 and min_culmination=10 on SatNOGS.
    # Matching its horizon is what makes our AOS/LOS agree with the schedule
    # SatNOGS actually records to — at 5 deg we were consistently ~80 s late.
    min_elevation_deg: float = 0.0
    # Below this peak elevation SatNOGS will not schedule a pass; we still
    # show them, marked, because a manual observation may still be wanted.
    min_culmination_deg: float = 10.0
    timezone: str = "Asia/Bangkok"

    # --- satellites ---------------------------------------------------------
    default_norad: int = 67683          # KNACKSAT-2
    pinned_norad: str = "67683"
    celestrak_group: str = "amateur"
    tle_ttl_s: int = 7200               # Celestrak 403s below this. Do not lower.
    tle_stale_warn_d: float = 7.0
    tle_stale_crit_d: float = 14.0

    # --- rotator ------------------------------------------------------------
    rotctld_host: str = "10.90.36.140"
    rotctld_port: int = 4533            # rotctld default; 4532 is rigctld (a RADIO)
    rotctld_protocol: str = "auto"      # auto | rotctld | rigctld
    rotator_poll_hz: float = 1.0
    rotator_backoff_min_s: float = 5.0
    rotator_backoff_max_s: float = 10.0
    mock_fault_period_s: float = 600.0

    # --- rotator control ----------------------------------------------------
    rotator_control_enabled: bool = False
    control_lease_s: int = 900
    gate_guard_s: int = 300
    gate_max_stale_s: int = 120
    track_deadband_deg: float = 2.0
    park_az: float = 0.0
    park_el: float = 0.0

    # --- cameras ------------------------------------------------------------
    go2rtc_url: str = "http://video:1984"
    camera_host: str = "10.90.36.130"

    # --- grafana ------------------------------------------------------------
    grafana_base: str = "https://dashboard.knacksat.com/telemetry"
    grafana_uid: str = "knacksat-telemetry-optimized-v3"
    grafana_slug: str = "knacksat-satellite-telemetry-monitor"
    grafana_panels: str = "200,1,3,6"
    grafana_range: str = "now-6h"
    # Template variables their dashboard needs. DS_INFLUXDB selects the
    # datasource; without it the panels render empty.
    grafana_vars: str = "DS_INFLUXDB=influxdb,callsign=All,gs_id=All"
    grafana_token: str = ""

    # --- satnogs ------------------------------------------------------------
    satnogs_network: str = "https://network.satnogs.org/api"
    satnogs_db: str = "https://db.satnogs.org/api"
    satnogs_jobs_poll_s: int = 60
    satnogs_obs_poll_s: int = 120
    satnogs_station_poll_s: int = 60
    satnogs_db_token: str = ""

    # --- derived ------------------------------------------------------------
    @property
    def pinned_norad_ids(self) -> list[int]:
        return _csv_ints(self.pinned_norad)

    @property
    def grafana_panel_ids(self) -> list[int]:
        return _csv_ints(self.grafana_panels)

    @property
    def grafana_var_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.grafana_vars.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    @property
    def tle_cache_path(self) -> Path:
        return self.data_dir / "tle_cache.json"

    @property
    def rotator_poll_interval_s(self) -> float:
        return 1.0 / max(0.1, self.rotator_poll_hz)


@lru_cache
def get_settings() -> Settings:
    return Settings()
