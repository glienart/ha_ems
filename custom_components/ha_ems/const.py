"""Constants for the HA EMS integration."""

DOMAIN = "ha_ems"
NAME = "Home Energy Management System"
VERSION = "0.3.0"

# Config entry keys — entity IDs provided by the user during setup
CONF_SOLAR_POWER = "solar_power_sensor"           # W, positive = producing
CONF_GRID_POWER = "grid_power_sensor"             # W, positive = importing
CONF_BATTERY_SOC = "battery_soc_sensor"           # %, 0–100
CONF_BATTERY_CHARGE_SWITCH = "battery_charge_switch"    # switch entity
CONF_BATTERY_DISCHARGE_SWITCH = "battery_discharge_switch"
CONF_BATTERY_STANDBY_SWITCH = "battery_standby_switch"  # optional
CONF_BATTERY_MAX_CHARGE_POWER = "battery_max_charge_power"   # W
CONF_BATTERY_MAX_DISCHARGE_POWER = "battery_max_discharge_power"  # W
CONF_BATTERY_MIN_SOC = "battery_min_soc"          # %, default 10
CONF_BATTERY_MAX_SOC = "battery_max_soc"          # %, default 95

CONF_EV_CHARGER_SWITCH = "ev_charger_switch"      # switch entity
CONF_EV_SOC = "ev_soc_sensor"                     # %, optional
CONF_EV_TARGET_SOC = "ev_target_soc"              # %, default 80
CONF_EV_DEPARTURE_TIME = "ev_departure_time"      # HH:MM, default 07:00
CONF_EV_MAX_CHARGE_POWER = "ev_max_charge_power"  # W

CONF_TARIFF_SENSOR = "tariff_sensor"              # €/kWh current price
CONF_CHEAP_TARIFF_THRESHOLD = "cheap_tariff_threshold"   # €/kWh
CONF_EXPENSIVE_TARIFF_THRESHOLD = "expensive_tariff_threshold"  # €/kWh

CONF_HOUSE_POWER = "house_power_sensor"           # W, optional — computed if absent
CONF_UPDATE_INTERVAL = "update_interval"          # seconds, default 60

# EMS operating modes
MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_ECO = "eco"       # maximise self-consumption, ignore tariff
MODE_CHEAP = "cheap"   # maximise grid charging when cheap
MODE_OFF = "off"       # EMS hands off, no control

EMS_MODES = [MODE_AUTO, MODE_ECO, MODE_CHEAP, MODE_MANUAL, MODE_OFF]

# Default values
DEFAULT_UPDATE_INTERVAL = 60       # seconds
DEFAULT_BATTERY_MIN_SOC = 10       # %
DEFAULT_BATTERY_MAX_SOC = 95       # %
DEFAULT_EV_TARGET_SOC = 80         # %
DEFAULT_EV_DEPARTURE_TIME = "07:00"
DEFAULT_CHEAP_THRESHOLD = 0.10     # €/kWh
DEFAULT_EXPENSIVE_THRESHOLD = 0.25 # €/kWh

# Decision keys (used in coordinator data)
DECISION_BATTERY = "battery_decision"
DECISION_EV = "ev_decision"
SOLAR_SURPLUS = "solar_surplus_w"
NET_POWER = "net_power_w"
CURRENT_MODE = "current_mode"

# Battery decisions
BAT_CHARGE = "charge"
BAT_DISCHARGE = "discharge"
BAT_STANDBY = "standby"
BAT_IDLE = "idle"

# EV decisions
EV_CHARGE = "charge"
EV_PAUSE = "pause"

# ── EPEX SPOT / ENTSO-E ────────────────────────────────────────────────────
CONF_EPEX_ZONE  = "epex_zone"   # ENTSO-E bidding zone EIC code
CONF_EPEX_TOKEN = "epex_token"  # ENTSO-E security token (free — register at entsoe.eu)

# EPEX SPOT bidding zones (EIC codes) — label → EIC
EPEX_ZONES: dict[str, str] = {
    "Belgium (BE)":                  "10YBE----------2",
    "France (FR)":                   "10YFR-RTE------C",
    "Germany / Luxembourg (DE-LU)":  "10Y1001A1001A63L",
    "Netherlands (NL)":              "10YNL----------L",
    "Austria (AT)":                  "10YAT-APG------L",
    "Switzerland (CH)":              "10YCH-SWISSGRID4",
    "Spain (ES)":                    "10YES-REE------0",
    "Portugal (PT)":                 "10YPT-REN------W",
    "Italy North (IT-North)":        "10Y1001A1001A73I",
    "Denmark DK1 (West)":            "10YDK-1--------W",
    "Denmark DK2 (East)":            "10YDK-2--------M",
    "Sweden SE3":                    "10Y1001A1001A46L",
    "Norway NO2":                    "10YNO-2--------T",
    "Finland (FI)":                  "10YFI-1--------U",
    "Poland (PL)":                   "10YPL-AREA-----S",
    "Czech Republic (CZ)":           "10YCZ-CEPS-----N",
}

# Reverse map: EIC → label
EPEX_ZONE_LABELS: dict[str, str] = {v: k for k, v in EPEX_ZONES.items()}
