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
    # Transmitters change on the scale of months, so this is deliberately long.
    transmitter_ttl_s: int = 86400
    # Frames only appear while the satellite is overhead and someone is
    # listening, so a few minutes is as often as there is any point asking.
    telemetry_ttl_s: int = 300
    # A waterfall only appears once a pass has finished and the station has
    # uploaded it. Asking more often than this cannot learn anything, and
    # asking on every request is what made SatNOGS answer 429.
    waterfall_ttl_s: int = 300

    # --- rotator ------------------------------------------------------------
    rotctld_host: str = "10.90.36.140"
    rotctld_port: int = 4533            # rotctld default; 4532 is rigctld (a RADIO)
    rotctld_protocol: str = "auto"      # auto | rotctld | rigctld

    # Station 5024's rigctld runs a Hamlib "Dummy" rig on 4534. satnogs-client
    # writes the Doppler-corrected downlink to it during a pass, so reading it
    # back is the only live view of what the receiver is actually tuned to.
    # Read-only: this dashboard never sets a frequency.
    rigctld_host: str = "10.90.36.140"
    rigctld_port: int = 4534
    rigctld_enabled: bool = True

    # The rotator's REAL travel limits, which are not what dump_caps reports.
    # dump_caps returns the SPID backend's compiled range (el -20..210); the
    # limits actually in force are rotctld's own -C overrides and the ones
    # satnogs-client pushes, and on station 5024 those are tighter:
    #   rotctld   -C min_az=-180,max_az=540,min_el=0,max_el=100
    #   satnogs   SATNOGS_ROT_SET_CONF min_az=-90,max_az=450,min_el=-5,max_el=100
    # Clamping to dump_caps would let a command through at an elevation the
    # hardware will not go to. These are the narrower, authoritative numbers.
    rot_limit_min_az: float = -90.0
    rot_limit_max_az: float = 450.0
    rot_limit_min_el: float = 0.0
    rot_limit_max_el: float = 100.0
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

    # --- schedule -------------------------------------------------------
    # The autoscheduler's own history-pages default (300 obs, ~75s uncached)
    # is what makes triggering a run fire-and-poll rather than blocking.
    # schedule_poll_s and schedule_hours now only SEED a fresh
    # schedule_config.json. Once one exists the operator's own Auto run
    # settings are authoritative and changing these does nothing.
    schedule_poll_s: int = 1800
    # 24h rather than the 48h this used to be: it matches run_scheduler.ps1,
    # the script that has actually been booking this station's passes.
    schedule_hours: float = 24.0
    schedule_history_pages: int = 12
    # Backstops on the satnogs-auto-scheduler child process, not tuning knobs.
    # A cold-cache run genuinely takes minutes, and that tool's booking POST
    # carries no timeout of its own - which is what the idle one is aimed at.
    schedule_timeout_s: int = 1800
    schedule_idle_timeout_s: int = 900
    # "module" starts the child through sys.executable: no PATH needed, and it
    # lets us pre-seed logging so severity survives the tool's own
    # format="%(message)s". "script" uses the installed console script instead.
    schedule_launcher: str = "module"
    # None (default) means "follow the global mock flag". Set explicitly to run
    # the REAL satnogs-auto-scheduler while every other component stays
    # simulated - the same escape hatch campaign_mock provides, and needed for
    # the same reason: GS_MOCK=0 would also open a live connection to the
    # station's rotctld, which is the one thing this station's setup
    # deliberately avoids (and right now the SPID rotator is powered off).
    #
    # Note this switch decides whether a run SPAWNS the tool. It does not
    # decide whether that run books: that is `dry_run`, per request.
    schedule_mock: bool | None = None

    # --- network campaign -------------------------------------------------
    # Daily, not more often: SatNOGS itself won't accept a booking more than
    # ~48h out, so there is nothing new to book within the same day anyway.
    campaign_poll_s: int = 86400
    # KNACKSAT-2 rarely has more than a couple of useful passes at any one
    # station within a 48h window - this is a backstop, not a tuning knob
    # meant to be raised casually.
    campaign_max_per_station: int = 2
    # Bounds the worst case (~160+ candidate stations) well below anything
    # that could look like spamming the community's shared stations from one
    # run, without hardcoding today's exact station count.
    campaign_max_total: int = 150
    # None (default) means "follow the global mock flag". Set explicitly to
    # run Network Campaign against real SatNOGS Network while every other
    # component (rotator, camera, the Station Schedule tab) stays mocked -
    # e.g. a dev box that must not open a second live connection to a
    # station's real rotctld.
    campaign_mock: bool | None = None

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
