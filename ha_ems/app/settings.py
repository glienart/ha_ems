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
from dataclasses import asdict, dataclass

_LOGGER = logging.getLogger(__name__)

OPTIONS_PATH = "/data/options.json"   # written by HA from config tab
RUNTIME_PATH = "/data/settings.json"  # written by the app (mode, overrides)


@dataclass
class EmsSettings:
    solar_power_sensor: str = ""
    grid_power_sensor: str = ""
    house_power_sensor: str = ""
    battery_soc_sensor: str = ""
    battery_power_sensor: str = ""   # W, + = charging, - = discharging
    battery_charge_switch: str = ""
    battery_discharge_switch: str = ""
    battery_standby_switch: str = ""
    battery_max_charge_w: int = 3000
    battery_max_discharge_w: int = 3000
    battery_min_soc: int = 10
    battery_max_soc: int = 95
    ev_charger_switch: str = ""
    ev_soc_sensor: str = ""
    ev_target_soc: int = 80
    ev_departure_time: str = "07:00"
    ev_max_charge_w: int = 7400
    tariff_sensor: str = ""
    # Effective price = a × EPEX + b  (b absorbs grid fees, taxes, margins)
    tariff_a_consumption: float = 1.0   # multiplier for buy price
    tariff_b_consumption: float = 0.0   # fixed offset  for buy price (€/kWh)
    tariff_a_injection: float = 1.0     # multiplier for sell/inject price
    tariff_b_injection: float = 0.0     # fixed offset  for sell/inject price (€/kWh)
    cheap_threshold: float = 0.10
    expensive_threshold: float = 0.25
    update_interval: int = 60
    mode: str = "auto"
    # EPEX SPOT — configured via HA add-on Configuration tab
    epex_token: str = ""
    epex_zone: str = "BE"  # short code: BE, FR, DE-LU, NL, AT, CH…


def load() -> EmsSettings:
    s = EmsSettings()

    # 1. Load entity config from HA options tab
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

    # 2. Override with runtime settings (mode, threshold tweaks from dashboard)
    if os.path.exists(RUNTIME_PATH):
        try:
            with open(RUNTIME_PATH) as f:
                rt = json.load(f)
            for k, v in rt.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except Exception as exc:
            _LOGGER.error("Failed to read settings.json: %s", exc)

    return s


def save_runtime(settings: EmsSettings) -> None:
    """Save all dashboard-managed settings to settings.json.

    EPEX fields (epex_token, epex_zone) are excluded — they come from
    the HA add-on Configuration tab (options.json) and must not be
    overwritten here.
    """
    exclude = {"epex_token", "epex_zone"}
    data = {k: v for k, v in asdict(settings).items() if k not in exclude}
    try:
        with open(RUNTIME_PATH, "w") as f:
            json.dump(data, f, indent=2)
        _LOGGER.info("Settings saved to %s", RUNTIME_PATH)
    except Exception as exc:
        _LOGGER.error("Failed to save settings.json: %s", exc)
