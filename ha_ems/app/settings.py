"""
Settings loader for HA EMS add-on.

HA writes the Configuration tab values to /data/options.json.
We read from there first, then fall back to /data/settings.json
for runtime changes (e.g. mode changes from the dashboard).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

_LOGGER = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"   # written by HA from config tab
RUNTIME_PATH = "/data/settings.json"  # written by the app (mode, overrides)


@dataclass
class EmsSettings:
    solar_power_sensor: str = ""
    grid_power_sensor: str = ""
    house_power_sensor: str = ""
    battery_soc_sensor: str = ""
    battery_power_sensor: str = ""
    # Energy meters (kWh, cumulative / total_increasing) — preferred over
    # integrating power. Any left empty falls back to integrating the matching
    # power sensor. Cost/revenue in € are always computed from EPEX tariffs.
    grid_import_energy_sensor: str = ""
    grid_export_energy_sensor: str = ""
    solar_energy_sensor: str = ""
    house_energy_sensor: str = ""
    battery_charge_energy_sensor: str = ""
    battery_discharge_energy_sensor: str = ""
    battery_charge_switch: str = ""
    battery_discharge_switch: str = ""
    battery_standby_switch: str = ""
    battery_max_charge_w: int = 3000
    battery_max_discharge_w: int = 3000
    battery_min_soc: int = 10
    battery_max_soc: int = 95
    evs: list = field(default_factory=list)
    tariff_sensor: str = ""
    tariff_a_consumption: float = 1.0
    tariff_b_consumption: float = 0.0
    tariff_a_injection: float = 1.0
    tariff_b_injection: float = 0.0
    cheap_threshold: float = 0.10
    expensive_threshold: float = 0.25
    cheap_hysteresis: float = 0.01
    expensive_hysteresis: float = 0.01
    cheap_lookahead_slots: int = 4
    update_interval: int = 60
    mode: str = "auto"
    epex_token: str = ""
    epex_zone: str = "BE"
    # Solar forecast & optimization
    latitude: float = 0.0
    longitude: float = 0.0
    panel_kwp: float = 0.0
    panel_tilt: int = 35
    panel_azimuth: int = 0
    battery_capacity_kwh: float = 10.0


def load() -> EmsSettings:
    s = EmsSettings()

    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH) as f:
                opts = json.load(f)
            for k, v in opts.items():
                if hasattr(s, k) and v != "":
                    setattr(s, k, v)
            _LOGGER.info("Loaded options from %s", OPTIONS_PATH)
        except Exception as exc:
            _LOGGER.error("Failed to read options.json: %s", exc)

    rt = {}
    if os.path.exists(RUNTIME_PATH):
        try:
            with open(RUNTIME_PATH) as f:
                rt = json.load(f)
            for k, v in rt.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception as exc:
            _LOGGER.error("Failed to read settings.json: %s", exc)

    # Migration: single EV fields (pre-0.5.5) -> evs list
    if not s.evs:
        old_switch = rt.get("ev_charger_switch", "")
        if old_switch:
            s.evs = [{
                "name": "EV",
                "charger_switch": old_switch,
                "soc_sensor": rt.get("ev_soc_sensor", ""),
                "target_soc": int(rt.get("ev_target_soc", 80)),
                "departure_time": rt.get("ev_departure_time", "07:00"),
                "max_charge_w": int(rt.get("ev_max_charge_w", 7400)),
            }]
            _LOGGER.info("Migrated single-EV config to evs list")

    return s


def save_runtime(settings: EmsSettings) -> None:
    exclude = {"epex_token", "epex_zone"}
    data = {k: v for k, v in asdict(settings).items() if k not in exclude}
    try:
        with open(RUNTIME_PATH, "w") as f:
            json.dump(data, f, indent=2)
        _LOGGER.info("Settings saved to %s", RUNTIME_PATH)
    except Exception as exc:
        _LOGGER.error("Failed to save settings.json: %s", exc)
