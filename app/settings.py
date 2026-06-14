"""
Persistent settings stored in /data/settings.json (add-on data volume).
Falls back to defaults if the file doesn't exist yet.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

_LOGGER = logging.getLogger(__name__)
SETTINGS_PATH = "/data/settings.json"


@dataclass
class EmsSettings:
    # Power sensors (entity IDs)
    solar_power_sensor: str = ""
    grid_power_sensor: str = ""
    house_power_sensor: str = ""

    # Battery
    battery_soc_sensor: str = ""
    battery_charge_switch: str = ""
    battery_discharge_switch: str = ""
    battery_standby_switch: str = ""
    battery_max_charge_w: int = 3000
    battery_max_discharge_w: int = 3000
    battery_min_soc: int = 10
    battery_max_soc: int = 95

    # EV
    ev_charger_switch: str = ""
    ev_soc_sensor: str = ""
    ev_target_soc: int = 80
    ev_departure_time: str = "07:00"
    ev_max_charge_w: int = 7400

    # Tariff
    tariff_sensor: str = ""
    cheap_threshold: float = 0.10
    expensive_threshold: float = 0.25

    # EMS
    mode: str = "auto"
    update_interval: int = 60


def load() -> EmsSettings:
    if not os.path.exists(SETTINGS_PATH):
        return EmsSettings()
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        s = EmsSettings()
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s
    except Exception as exc:
        _LOGGER.error("Failed to load settings: %s", exc)
        return EmsSettings()


def save(settings: EmsSettings) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(asdict(settings), f, indent=2)
    except Exception as exc:
        _LOGGER.error("Failed to save settings: %s", exc)
